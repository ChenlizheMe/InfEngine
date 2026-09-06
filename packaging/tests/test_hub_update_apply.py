from __future__ import annotations

import json
from pathlib import Path

import hub_update_apply
import pytest


def _metadata(path: Path, *, files: list[str], delete: list[str]) -> Path:
    path.write_text(
        json.dumps(
            {
                "$schema": "infernux.hub_update",
                "product": "InfernuxHub",
                "base_version": "0.4.0",
                "target_version": "0.4.1",
                "files": [{"path": value} for value in files],
                "delete": delete,
            }
        ),
        encoding="utf-8",
    )
    return path


def test_linux_applier_replaces_owned_files_and_restarts(tmp_path: Path, monkeypatch):
    install = tmp_path / "installed"
    stage = tmp_path / "stage"
    install.mkdir()
    stage.mkdir()
    (install / "Infernux Hub").write_bytes(b"old")
    (install / "removed.so").write_bytes(b"remove")
    (stage / "Infernux Hub").write_bytes(b"new")
    shared = install / "InfernuxHubData/Shared/Library/plugin.inxpkg"
    shared.parent.mkdir(parents=True)
    shared.write_bytes(b"user plugin")
    metadata = _metadata(
        tmp_path / "hub-update.json",
        files=["Infernux Hub"],
        delete=["removed.so"],
    )
    launches: list[tuple[list[str], dict[str, object]]] = []
    monkeypatch.setattr(hub_update_apply, "_wait_for_exit", lambda _pid: None)
    monkeypatch.setattr(
        hub_update_apply.subprocess,
        "Popen",
        lambda args, **kwargs: launches.append((args, kwargs)),
    )

    hub_update_apply.apply_update(
        parent_pid=12,
        install_dir=install,
        stage_dir=stage,
        metadata_path=metadata,
    )

    assert (install / "Infernux Hub").read_bytes() == b"new"
    assert shared.read_bytes() == b"user plugin"
    assert not (install / "removed.so").exists()
    assert launches == [
        (
            [str(install / "Infernux Hub")],
            {"cwd": install, "start_new_session": True},
        )
    ]


@pytest.mark.parametrize("operation", ["files", "delete"])
@pytest.mark.parametrize("relative", [
    "InfernuxHubData/Shared/Library/private.inxpkg",
    "infernuxhubdata/SHARED/PlatformKits/android/sdk.bin",
])
def test_updates_cannot_replace_or_delete_shared_resources(tmp_path, operation, relative):
    from hub_release import _safe_relative_path
    from hub_updater import _safe_path

    for validator in (_safe_relative_path, _safe_path):
        with pytest.raises(ValueError, match="shared resources"):
            validator(relative)
    metadata = _metadata(
        tmp_path / "hub-update.json",
        files=[relative] if operation == "files" else [],
        delete=[relative] if operation == "delete" else [],
    )
    with pytest.raises(ValueError, match="shared resources"):
        hub_update_apply.apply_update(
            parent_pid=1, install_dir=tmp_path / "installed",
            stage_dir=tmp_path / "stage", metadata_path=metadata,
        )


def test_linux_applier_rejects_paths_outside_the_installation(tmp_path: Path):
    metadata = _metadata(
        tmp_path / "hub-update.json",
        files=["../outside"],
        delete=[],
    )

    try:
        hub_update_apply.apply_update(
            parent_pid=1,
            install_dir=tmp_path / "installed",
            stage_dir=tmp_path / "stage",
            metadata_path=metadata,
        )
    except ValueError as exc:
        assert "Unsafe Hub update path" in str(exc)
    else:
        raise AssertionError("unsafe update path was accepted")


def test_linux_applier_rolls_back_when_update_omits_the_executable(
    tmp_path: Path, monkeypatch
):
    install = tmp_path / "installed"
    stage = tmp_path / "stage"
    install.mkdir()
    stage.mkdir()
    (install / "Infernux Hub").write_bytes(b"old")
    (install / "library.so").write_bytes(b"old-library")
    (stage / "library.so").write_bytes(b"new-library")
    metadata = _metadata(
        tmp_path / "hub-update.json",
        files=["library.so"],
        delete=["Infernux Hub"],
    )
    monkeypatch.setattr(hub_update_apply, "_wait_for_exit", lambda _pid: None)

    try:
        hub_update_apply.apply_update(
            parent_pid=12,
            install_dir=install,
            stage_dir=stage,
            metadata_path=metadata,
        )
    except FileNotFoundError as exc:
        assert "Updated Hub executable is missing" in str(exc)
    else:
        raise AssertionError("update without the Hub executable was accepted")

    assert (install / "Infernux Hub").read_bytes() == b"old"
    assert (install / "library.so").read_bytes() == b"old-library"
