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
        "bundle_runtime_dir": str(tmp_path / "hub" / "InfernuxHubData" / "runtime"),
        "default_version": DEFAULT_PYTHON_RUNTIME,
    }
    assert observed["ensure"] == {
        "version": DEFAULT_PYTHON_RUNTIME,
        "on_status": None,
        "allow_frozen_repair": True,
    }
