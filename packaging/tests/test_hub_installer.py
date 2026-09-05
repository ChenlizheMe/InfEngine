from __future__ import annotations

from pathlib import Path

import pytest

from installer.install_application import HubInstallTransaction
from installer import install_python_runtime
from installer.payload import (
    HUB_EXECUTABLE,
    HUB_PAYLOAD_ARCHIVE,
    create_payload_archive,
)
from installer_safety import write_install_marker
from python_runtime_catalog import DEFAULT_PYTHON_RUNTIME
import installer_gui


def _make_payload(path: Path, executable: bytes = b"new hub executable") -> Path:
    path.mkdir(parents=True)
    (path / HUB_EXECUTABLE).write_bytes(executable)
    (path / "hub-version.json").write_text('{"version": "0.2.9"}\n', encoding="utf-8")
    data_dir = path / "InfernuxHubData"
    data_dir.mkdir()
    (data_dir / "new-library.dll").write_bytes(b"new library")
    return path


def test_linux_application_default_is_separate_from_user_data(monkeypatch, tmp_path):
    monkeypatch.setattr(installer_gui.sys, "platform", "linux")
    monkeypatch.setattr(
        installer_gui.os.path,
        "expanduser",
        lambda value: str(tmp_path / value.removeprefix("~/")),
    )

    install_dir = Path(installer_gui._default_install_dir())
    user_data = tmp_path / ".local" / "share" / "InfernuxHub"

    assert install_dir == tmp_path / ".local" / "opt" / "InfernuxHub"
    assert install_dir != user_data


def test_transaction_replaces_executable_and_removes_stale_files(tmp_path: Path):
    payload = _make_payload(tmp_path / "payload")
    install_dir = tmp_path / "Infernux Hub"
    install_dir.mkdir()
    (install_dir / HUB_EXECUTABLE).write_bytes(b"old hub executable")
    (install_dir / "stale-library.dll").write_bytes(b"stale")
    write_install_marker(str(install_dir))

    with HubInstallTransaction(payload, install_dir) as installation:
        installation.prepare()
        installation.activate()
        installation.commit()

    assert (install_dir / HUB_EXECUTABLE).read_bytes() == b"new hub executable"
    assert (
        install_dir / "InfernuxHubData" / "new-library.dll"
    ).read_bytes() == b"new library"
    assert not (install_dir / "stale-library.dll").exists()


@pytest.mark.parametrize("rollback", [False, True])
def test_transaction_preserves_shared_resources_across_replacement(tmp_path, rollback):
    payload = _make_payload(tmp_path / "payload")
    install_dir = tmp_path / "Infernux Hub"
    install_dir.mkdir()
    (install_dir / HUB_EXECUTABLE).write_bytes(b"old hub executable")
    write_install_marker(str(install_dir))
    shared = install_dir / "InfernuxHubData/Shared"
    relative_files = [
        "Library/Plugins/example.inxpkg", "PlatformKits/android/sdk/tool.bin",
        "Runtimes/python313/python.exe", "Engines/040/engine.whl",
    ]
    for relative in relative_files:
        path = shared / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(relative.encode())
    identity = shared.stat().st_ino

    with HubInstallTransaction(payload, install_dir) as installation:
        installation.prepare()
        installation.activate()
        assert shared.stat().st_ino == identity  # transfer, never copy gigabytes
        for relative in relative_files:
            assert (shared / relative).read_bytes() == relative.encode()
        (shared / "added-during-install.txt").write_text("new runtime state")
        if not rollback:
            installation.commit()

    assert shared.stat().st_ino == identity
    for relative in relative_files:
        assert (shared / relative).read_bytes() == relative.encode()
    assert (shared / "added-during-install.txt").read_text() == "new runtime state"
    assert (install_dir / HUB_EXECUTABLE).read_bytes() == (
        b"old hub executable" if rollback else b"new hub executable"
    )
    assert not list(tmp_path.glob(".Infernux Hub.backup-*"))


def test_installer_payload_cannot_own_shared_resources(tmp_path):
    payload = _make_payload(tmp_path / "payload")
    shared = payload / "InfernuxHubData/Shared"
    shared.mkdir()
    (shared / "user-data.bin").write_bytes(b"must not ship")
    with pytest.raises(RuntimeError, match="shared resources"):
        with HubInstallTransaction(payload, tmp_path / "Hub") as installation:
            installation.prepare()
    assert not (tmp_path / "Hub").exists()


def test_failed_shared_transfer_restores_the_old_installation(tmp_path, monkeypatch):
    from installer import install_application

    payload = _make_payload(tmp_path / "payload")
    install_dir = tmp_path / "Hub"
    _make_payload(install_dir, executable=b"old executable")
    write_install_marker(str(install_dir))
    shared = install_dir / "InfernuxHubData/Shared"
    shared.mkdir()
    (shared / "keep.inxpkg").write_bytes(b"user package")
    replace = install_application.os.replace

    def fail_transfer(source, destination):
        if Path(source).name == "Shared":
            raise PermissionError("shared resources locked")
        return replace(source, destination)

    monkeypatch.setattr(install_application.os, "replace", fail_transfer)
    with pytest.raises(PermissionError, match="shared resources locked"):
        with HubInstallTransaction(payload, install_dir) as installation:
            installation.prepare()
            installation.activate()
    assert (shared / "keep.inxpkg").read_bytes() == b"user package"
    assert (install_dir / HUB_EXECUTABLE).read_bytes() == b"old executable"


def test_transaction_rolls_back_after_activation_error(tmp_path: Path):
    payload = _make_payload(tmp_path / "payload")
    install_dir = tmp_path / "Infernux Hub"
    install_dir.mkdir()
    (install_dir / HUB_EXECUTABLE).write_bytes(b"old hub executable")
    write_install_marker(str(install_dir))

    with pytest.raises(RuntimeError, match="simulated registration failure"):
        with HubInstallTransaction(payload, install_dir) as installation:
            installation.prepare()
            installation.activate()
            raise RuntimeError("simulated registration failure")

    assert (install_dir / HUB_EXECUTABLE).read_bytes() == b"old hub executable"


def test_transaction_rejects_unrecognized_nonempty_directory(tmp_path: Path):
    payload = _make_payload(tmp_path / "payload")
    install_dir = tmp_path / "Other Tools"
    install_dir.mkdir()
    existing_file = install_dir / "keep.txt"
    existing_file.write_text("user data", encoding="utf-8")

    with pytest.raises(
        RuntimeError, match="not a recognized Infernux Hub installation"
    ):
        with HubInstallTransaction(payload, install_dir) as installation:
            installation.prepare()

    assert existing_file.read_text(encoding="utf-8") == "user data"


def test_archived_payload_preserves_and_installs_binary_files(tmp_path: Path):
    source_payload = _make_payload(tmp_path / "source-payload")
    (source_payload / "Qt6Core.dll").write_bytes(b"qt binary")
    (source_payload / "shiboken6.pyd").write_bytes(b"python extension")
    embedded_payload = tmp_path / "embedded-payload"
    embedded_payload.mkdir()
    archive = create_payload_archive(
        source_payload,
        embedded_payload / HUB_PAYLOAD_ARCHIVE,
    )
    install_dir = tmp_path / "Infernux Hub"

    with HubInstallTransaction(embedded_payload, install_dir) as installation:
        installation.prepare()
        installation.activate()
        installation.commit()

    assert archive.is_file()
    assert (install_dir / HUB_EXECUTABLE).read_bytes() == b"new hub executable"
    assert (install_dir / "Qt6Core.dll").read_bytes() == b"qt binary"
    assert (install_dir / "shiboken6.pyd").read_bytes() == b"python extension"


def test_fresh_installer_deploys_only_the_default_python_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed: dict[str, object] = {}

    class _RuntimeManager:
        def __init__(self, **kwargs):
            observed["constructor"] = kwargs

        def ensure_runtime(self, **kwargs):
            observed["ensure"] = kwargs
            return str(tmp_path / "python313" / "python.exe")

    monkeypatch.setattr(
        install_python_runtime, "PythonRuntimeManager", _RuntimeManager
    )

    result = install_python_runtime.install_runtime_for_app(str(tmp_path / "hub"))

    assert result.endswith(str(Path("python313") / "python.exe"))
    assert observed["constructor"] == {
        "runtime_dir": str(tmp_path / "hub/InfernuxHubData/Shared/Runtimes"),
        "bundle_runtime_dir": str(tmp_path / "hub" / "InfernuxHubData" / "runtime"),
        "default_version": DEFAULT_PYTHON_RUNTIME,
    }
    assert observed["ensure"] == {
        "version": DEFAULT_PYTHON_RUNTIME,
        "on_status": None,
        "allow_frozen_repair": True,
    }


def test_windows_shared_permissions_target_only_installing_user_and_shared(tmp_path, monkeypatch):
    from types import SimpleNamespace

    commands = []
    monkeypatch.setattr(install_python_runtime.sys, "platform", "win32")
    monkeypatch.setenv("SystemRoot", str(tmp_path / "Windows"))

    def execute(command, **options):
        commands.append((command, options))
        return SimpleNamespace(stdout='"machine\\author","S-1-5-21-123-456-789-1001"\n')

    monkeypatch.setattr(install_python_runtime.subprocess, "run", execute)
    result = install_python_runtime.prepare_shared_storage(str(tmp_path / "Hub"))
    expected = str((tmp_path / "Hub/InfernuxHubData/Shared").resolve())
    assert result == expected
    assert commands[1][0] == [
        str(tmp_path / "Windows/System32/icacls.exe"), expected,
        "/grant", "*S-1-5-21-123-456-789-1001:(OI)(CI)M",
    ]
    assert all(options["check"] and options["creationflags"] == 0x08000000 for _, options in commands)


def test_installer_reuses_live_shared_runtime_after_activation(tmp_path, monkeypatch):
    import installer_gui

    payload = _make_payload(tmp_path / "payload")
    install = _make_payload(tmp_path / "Hub", executable=b"old")
    write_install_marker(str(install))
    existing = install / "InfernuxHubData/Shared/Runtimes/python313/marker"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"existing runtime")
    calls = []
    monkeypatch.setattr(installer_gui, "_payload_dir", lambda: str(payload))

    def install_runtime(app_dir, **kwargs):
        assert app_dir == str(install)
        assert (install / HUB_EXECUTABLE).read_bytes() == b"new hub executable"
        assert existing.read_bytes() == b"existing runtime"
        calls.append(app_dir)

    monkeypatch.setattr(installer_gui, "install_runtime_for_app", install_runtime)
    monkeypatch.setattr(installer_gui, "_write_registry", lambda path: None)
    monkeypatch.setattr(installer_gui, "_create_start_menu_shortcut", lambda path: None)
    errors = []
    worker = installer_gui.InstallWorker(str(install))
    worker.error.connect(errors.append)
    worker.run()
    assert not errors
    assert calls == [str(install)]
    assert existing.read_bytes() == b"existing runtime"
