from __future__ import annotations

import json
import os

import pytest

from Infernux import engine as engine_module


def test_engine_ready_file_is_a_required_handshake_when_configured(
    tmp_path,
    monkeypatch,
):
    missing_parent = tmp_path / "missing" / "ready.txt"
    monkeypatch.setenv("_INFERNUX_READY_FILE", str(missing_parent))

    with pytest.raises(FileNotFoundError):
        engine_module._signal_engine_loaded()


def test_current_process_is_running():
    assert engine_module._is_pid_running(os.getpid()) is True


def test_project_lock_rejects_malformed_json(tmp_path, monkeypatch):
    project = tmp_path / "Project"
    settings = project / "ProjectSettings"
    settings.mkdir(parents=True)
    lock_path = settings / ".infernux-engine-lock.json"
    lock_path.write_text("{", encoding="utf-8")
    monkeypatch.delenv("_INFERNUX_PROJECT_LOCK_PATH", raising=False)
    monkeypatch.delenv("_INFERNUX_PROJECT_LOCK_TOKEN", raising=False)

    with pytest.raises(json.JSONDecodeError):
        engine_module._acquire_project_lock(str(project), "editor")

    assert lock_path.read_text(encoding="utf-8") == "{"


def test_project_lock_replaces_a_stopped_owner(tmp_path, monkeypatch):
    project = tmp_path / "Project"
    settings = project / "ProjectSettings"
    settings.mkdir(parents=True)
    lock_path = settings / ".infernux-engine-lock.json"
    lock_path.write_text(
        json.dumps({"pid": 12345, "token": "stopped", "mode": "editor"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("_INFERNUX_PROJECT_LOCK_TOKEN", "replacement")
    monkeypatch.delenv("_INFERNUX_PROJECT_LOCK_PATH", raising=False)
    monkeypatch.setattr(engine_module, "_is_pid_running", lambda _pid: False)

    acquired_path, token = engine_module._acquire_project_lock(str(project), "editor")

    assert acquired_path == str(lock_path)
    assert token == "replacement"
    payload = json.loads(lock_path.read_text(encoding="utf-8"))
    assert payload["pid"] == os.getpid()
    assert payload["token"] == "replacement"
    engine_module._remove_project_lock(acquired_path, token)


def test_project_lock_removal_reports_persistent_permission_failure(
    tmp_path,
    monkeypatch,
):
    lock_path = tmp_path / "project-lock.json"
    lock_path.write_text(
        json.dumps({"pid": os.getpid(), "token": "owned"}),
        encoding="utf-8",
    )
    attempts = []

    def reject_remove(_path):
        attempts.append(1)
        raise PermissionError("lock is held")

    monkeypatch.setattr(engine_module.os, "remove", reject_remove)
    monkeypatch.setattr(engine_module.time, "sleep", lambda _seconds: None)

    with pytest.raises(PermissionError, match="lock is held"):
        engine_module._remove_project_lock(str(lock_path), "owned")

    assert len(attempts) == 20
